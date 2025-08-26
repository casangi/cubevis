import webbrowser

def have_firefox( ):
    """Check if any variant of Firefox will be used."""
    browser = webbrowser.get()
    firefox_indicators = ['firefox', 'mozilla']
    return any(indicator in browser.name.lower() for indicator in firefox_indicators)
